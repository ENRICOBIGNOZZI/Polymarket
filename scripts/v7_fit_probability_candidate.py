#!/usr/bin/env python3
"""Experimental settlement probability fit; never authorizes real execution.

Targets include resolved submitted orders, filled or not. Markets receive equal
weight. Chronological diagnostics are not prospective proof or an execution
backtest. Coefficient uncertainty is block-bootstrap model uncertainty, not a
certified conditional-probability confidence interval.
"""
from __future__ import annotations
import argparse, hashlib, json, math, time
from pathlib import Path
import numpy as np

ASSETS=('BTC','ETH','SOL','XRP','DOGE','BNB')
HORIZONS=('M5','M15','H1','H4','D1')
FEATURES=('intercept','market_logit','absolute_shock_scaled','confirmation_scaled',
 'log_signal_age','log_tte','spread_ticks','selected_imbalance',
 'asset_BTC','asset_ETH','asset_SOL','asset_XRP','asset_DOGE','asset_BNB',
 'horizon_M15','horizon_H1','horizon_H4','horizon_D1')

def feature_vector(r, scales):
    f=r['features'];a=r['asset'];h=r['horizon'];direction=int(f['direction'])
    if direction not in (-1,1):raise ValueError('direction')
    bid=float(f['bid_e4']);ask=float(f['ask_e4'])
    if not 0<bid<=ask<10000:raise ValueError('invalid two-sided book')
    pm=min(.9999,max(.0001,(bid+ask)/20000))
    scale=float(scales[ASSETS.index(a)])
    if not math.isfinite(scale) or scale<=0:raise ValueError('shock scale')
    age=float(f['signal_age_ns'])/1e6;tte=float(f['tte_ns'])/1e9
    total=float(f['bid_quantity'])+float(f['ask_quantity'])
    if age<0 or tte<=0 or total<=0:raise ValueError('invalid time/depth')
    x=[1.,math.log(pm/(1-pm)),min(10.,abs(float(f['binance_return_100ms_bp']))/scale),
       max(-10.,min(10.,direction*float(f['coinbase_return_100ms_bp'])/scale)),
       min(2.,math.log1p(age)/math.log(5001.)),max(-3.,min(3.,math.log(tte/120.))),
       min(20.,(ask-bid)/100.),(float(f['bid_quantity'])-float(f['ask_quantity']))/total]
    x += [float(a==v) for v in ASSETS]
    x += [float(h==v) for v in HORIZONS[1:]]
    if not all(math.isfinite(v) for v in x):raise ValueError('nonfinite feature')
    return np.asarray(x,dtype=float)

def sigmoid(z):
    z=np.clip(z,-40,40)
    return 1./(1.+np.exp(-z))

def fit(X,y,w,penalty):
    prior=np.zeros(X.shape[1]);prior[1]=1.;b=prior.copy()
    for _ in range(60):
        q=sigmoid(X@b)
        gradient=X.T@(w*(q-y))+penalty*(b-prior)
        H=X.T@((w*q*(1-q))[:,None]*X)+np.diag(penalty)
        step=np.linalg.solve(H,gradient)
        old=np.sum(w*(np.logaddexp(0,X@b)-y*(X@b)))+.5*np.sum(penalty*(b-prior)**2)
        eta=1.
        while eta>1e-7:
            trial=b-eta*step;z=X@trial
            loss=np.sum(w*(np.logaddexp(0,z)-y*z))+.5*np.sum(penalty*(trial-prior)**2)
            if loss<=old+1e-10:break
            eta*=.5
        b=trial
        if np.linalg.norm(eta*step)<1e-8:break
    return b

def weights(rows):
    from collections import Counter
    c=Counter(r['market_id'] for r in rows)
    return np.asarray([1./c[r['market_id']] for r in rows])

def scales_for(rows):
    all_values=[abs(r['features']['binance_return_100ms_bp']) for r in rows]
    pooled=float(np.median(all_values))
    return [float(np.median([abs(r['features']['binance_return_100ms_bp']) for r in rows if r['asset']==a])) if any(r['asset']==a for r in rows) else pooled for a in ASSETS]

def metrics(q,y,w):
    q=np.clip(q,1e-8,1-1e-8);s=w.sum()
    return {'log_loss':float(-np.sum(w*(y*np.log(q)+(1-y)*np.log(1-q)))/s),
            'brier':float(np.sum(w*(q-y)**2)/s),'effective_market_weight':float(s)}

def train(rows, output, code_sha, seed=20260919):
    usable=[];excluded=0
    for r in rows:
        if r.get('selected_outcome') not in (0.0,1.0) or r.get('feature_join')!='UNIQUE':excluded+=1;continue
        try:feature_vector(r,[1.]*6)
        except (ValueError,KeyError,TypeError):excluded+=1;continue
        usable.append(r)
    usable.sort(key=lambda r:(r['close_ts_ms'],r['market_id'],r['decision_ts_ms']))
    blocks=sorted({r['close_ts_ms']//900000 for r in usable})
    if len(blocks)<4:raise ValueError('insufficient 15-minute time blocks')
    split=blocks[max(1,int(.7*len(blocks)))];training=[r for r in usable if r['close_ts_ms']//900000<split]
    validation=[r for r in usable if r['close_ts_ms']//900000>=split]
    # Purge contracts whose close overlaps the earliest held-out decision.
    first_decision=min(r['decision_ts_ms'] for r in validation)
    training=[r for r in training if r['close_ts_ms']<first_decision]
    penalty=np.array([3.,8.,8.,8.,8.,8.,8.,8.]+[12.]*6+[12.]*4)
    s=scales_for(training);X=np.vstack([feature_vector(r,s) for r in training]);y=np.array([r['selected_outcome'] for r in training]);w=weights(training)
    b=fit(X,y,w,penalty);V=np.vstack([feature_vector(r,s) for r in validation]);vy=np.array([r['selected_outcome'] for r in validation]);vw=weights(validation)
    diagnostics={'training_orders':len(training),'training_markets':len({r['market_id'] for r in training}),
      'validation_orders':len(validation),'validation_markets':len({r['market_id'] for r in validation}),
      'held_out_market_overlap':len({r['market_id'] for r in training}&{r['market_id'] for r in validation}),
      'chronological_model':metrics(sigmoid(V@b),vy,vw),'chronological_market_mid':metrics(sigmoid(V[:,1]),vy,vw),
      'historical_label_availability_verified':False,'prospective_validation_complete':False,
      'warning':'Retrospective, market-blocked diagnostic. No executable PnL claim. Prior analysis saw this history.'}
    # Freeze the prospective candidate using everything actually available now.
    s=scales_for(usable);X=np.vstack([feature_vector(r,s) for r in usable]);y=np.array([r['selected_outcome'] for r in usable]);w=weights(usable)
    b=fit(X,y,w,penalty);block=np.array([r['close_ts_ms']//900000 for r in usable]);unique=np.unique(block)
    rng=np.random.default_rng(seed);draws=[]
    for _ in range(128):
        selected=rng.choice(unique,len(unique),replace=True);counts={v:int(np.sum(selected==v)) for v in unique}
        boot_w=w*np.array([counts[v] for v in block]);draws.append(fit(X,y,boot_w,penalty))
    covariance=np.cov(np.asarray(draws),rowvar=False,ddof=1)
    artifact={'schema':'v7_probability_logit_candidate_v1','feature_schema':list(FEATURES),
      'code_sha':code_sha,'created_unix':time.time(),'training_data_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
      'coefficients':b.tolist(),'covariance':covariance.tolist(),'shock_scales':s,
      'asset_order':list(ASSETS),'horizon_order':list(HORIZONS),'training_orders':len(usable),
      'training_markets':len({r['market_id'] for r in usable}),'excluded_orders':excluded,
      'time_blocks':len(unique),'uncertainty_z':1.645,'explicit_logit_reserve':.25,
      'uncertainty_semantics':'15MIN_BLOCK_BOOTSTRAP_MODEL_COVARIANCE_PLUS_EXPLICIT_LOGIT_RESERVE_NOT_COVERAGE_CERTIFIED',
      'forward_calibrated':False,'parameters_empirically_fitted':True,'paper_only':True,
      'prediction_target':'SETTLEMENT_PAYOFF_GIVEN_DECISION_FEATURES_NOT_FILL_CONDITIONED',
      'proposal_population':'HISTORICAL_SUBMITTED_ORDERS_ONLY',
      'fill_conditioning_validated':False,'action_change_transportability_validated':False,
      'real_order_submission':False,'authenticated_execution':False,
      'test_duration_seconds':7200,'excluded_assets':[],'asset_shadow_overrides':[],
      'maximum_order_cost_microdollars':3750000,'maximum_quantity_microunits':20000000,
      'execution_reserve_per_share':.005,'execution_reserve_is_measured':False,
      'diagnostics':diagnostics}
    output.write_text(json.dumps(artifact,indent=2,allow_nan=False))
    return artifact

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--orders',type=Path,required=True);a.add_argument('--output',type=Path,required=True);a.add_argument('--code-sha',required=True);ns=a.parse_args()
    x=train(json.loads(ns.orders.read_text()),ns.output,ns.code_sha)
    print(json.dumps({'training_orders':x['training_orders'],'training_markets':x['training_markets'],'time_blocks':x['time_blocks'],'diagnostics':x['diagnostics'],'coefficients':dict(zip(FEATURES,x['coefficients']))},indent=2))
