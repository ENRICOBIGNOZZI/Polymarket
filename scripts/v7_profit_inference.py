"""Contract-level inference with explicit temporal and Monte Carlo limits.

Inputs must be chronological within-contract means. The output never grants
promotion. An inspected historical protocol is not retrospectively registered.
"""
from __future__ import annotations
import math
import random


def quantile(values,p):
    if not values:return None
    x=p*(len(values)-1);lo=int(x);hi=min(lo+1,len(values)-1)
    return values[lo]+(values[hi]-values[lo])*(x-lo)


def describe(values):
    values=list(values)
    if any(not math.isfinite(v) for v in values):raise ValueError('nonfinite economic observation')
    ordered=sorted(values)
    return {'contracts':len(values),'mean':sum(values)/len(values) if values else None,
        'median':quantile(ordered,.5),'p05':quantile(ordered,.05),'p95':quantile(ordered,.95),
        'minimum':min(values) if values else None,'maximum':max(values) if values else None}


def interval(values,protocol,family):
    values=list(values);n=len(values);cfg=protocol['inference']
    if isinstance(family,bool) or not isinstance(family,int) or family<1:raise ValueError('invalid comparison family')
    repetitions=cfg['bootstrap_draws'];alpha=cfg['familywise_alpha']/family
    blocks=cfg.get('temporal_block_contracts',[3,6,12]);min_blocks=cfg.get('minimum_temporal_blocks',8)
    expected_tail=repetitions*alpha/2
    folds=cfg['chronological_folds'];parts=[values[i*n//folds:(i+1)*n//folds] for i in range(folds)]
    result={**describe(values),'interval':None,'state':'INSUFFICIENT_INDEPENDENT_CONTRACTS',
        'chronological_fold_means':[sum(x)/len(x) if x else None for x in parts],
        'method':'MOVING_CONTRACT_BLOCK_PERCENTILE_BOOTSTRAP_BONFERRONI_SENSITIVITY_ENVELOPE',
        'analysis_revision':'permanent-inference-v1','comparison_family_size':family,
        'bootstrap_draws':repetitions,'expected_draws_in_each_adjusted_tail':expected_tail,
        'minimum_tail_draws':cfg.get('minimum_tail_draws',50),'block_contracts':blocks,
        'inference_limit':'Approximate interval conditional on observed contracts. Temporal sensitivity is not proof of stationarity or absence of selection/censoring bias. No automatic promotion.'}
    if n<cfg['minimum_contracts_for_interval']:return result
    if expected_tail<result['minimum_tail_draws']:
        result['state']='INSUFFICIENT_MONTE_CARLO_TAIL_RESOLUTION';return result
    if n<max(blocks)*min_blocks:
        result['state']='INSUFFICIENT_TEMPORAL_BLOCKS';return result
    estimates={}
    for block in blocks:
        # Noncircular moving blocks do not falsely connect the last regime to
        # the first. Keep the exact sample length including a partial last block.
        prefix=[0.]
        for x in values:prefix.append(prefix[-1]+x)
        full=n//block;remainder=n%block;maximum_start=n-block
        sums=[prefix[i+block]-prefix[i] for i in range(maximum_start+1)]
        rng=random.Random(cfg['bootstrap_seed']+block);draws=[]
        for _ in range(repetitions):
            total=sum(rng.choices(sums,k=full))
            if remainder:
                start=rng.randrange(maximum_start+1);total+=prefix[start+remainder]-prefix[start]
            draws.append(total/n)
        draws.sort();estimates[str(block)]=[quantile(draws,alpha/2),quantile(draws,1-alpha/2)]
    result.update(state='ESTIMATED_EXPLORATORY_TEMPORAL_INTERVAL',block_sensitivity_intervals=estimates,
        interval=[min(x[0] for x in estimates.values()),max(x[1] for x in estimates.values())])
    return result
