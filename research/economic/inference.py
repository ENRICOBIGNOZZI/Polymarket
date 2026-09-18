"""Predeclared paired block inference; dependence and missingness stay explicit."""
from __future__ import annotations
from collections import defaultdict
from math import isfinite, ceil
from random import Random
from statistics import mean
from typing import Any


def paired_block_report(rows: list[dict[str, Any]], *, seed: int = 20260918,
                        repetitions: int = 4000, minimum_blocks: int = 24,
                        block_length: int = 3, minimum_improvement: float = 0.0,
                        required_complete_fraction: float = .99, alpha: float = .05,
                        family_size: int = 1) -> dict:
    """Circular moving-block bootstrap of time blocks, all assets kept together.

    Rows have block (chronologically sortable), market, baseline, candidate and
    complete. Zero is allowed only for an observed market with no trade.
    Confidence intervals are conditional on this dataset and protocol. No claim
    of exact finite-sample coverage is made.
    """
    if repetitions < 100 or minimum_blocks < 2 or block_length < 1 or not 0<alpha<1 or family_size<1:
        raise ValueError('invalid inference protocol')
    blocks: dict[str,list[float]]=defaultdict(list)
    seen=set();missing=0
    for row in rows:
        identity=(row['block'],row['market'])
        if identity in seen: raise ValueError('duplicate market/block')
        seen.add(identity)
        if row.get('complete') is not True or row.get('baseline') is None or row.get('candidate') is None:
            missing+=1;continue
        a,b=float(row['baseline']),float(row['candidate'])
        if not isfinite(a) or not isfinite(b): raise ValueError('nonfinite observations')
        blocks[str(row['block'])].append(b-a)
    values=[sum(blocks[key]) for key in sorted(blocks)]
    count=len(values);coverage=(len(rows)-missing)/len(rows) if rows else 0.0
    out={'state':'INCONCLUSIVE','paired_rows':len(rows)-missing,'missing_rows':missing,
        'complete_fraction':coverage,'observed_blocks':count,'minimum_blocks':minimum_blocks,
        'mean_incremental_pnl_per_block':mean(values) if values else None,
        'ci95_lower':None,'ci95_upper':None,'seed':seed,'repetitions':repetitions,
        'block_length':block_length,'minimum_improvement':minimum_improvement,
        'family_size':family_size,'familywise_alpha':alpha,'interval_alpha':alpha/family_size,
        'confidence_lower':None,'confidence_upper':None,
        'real_money_authorized':False,'automatic_promotion':False,'reasons':[]}
    if coverage < required_complete_fraction: out['reasons'].append('INSUFFICIENT_DATA_COVERAGE')
    if count < minimum_blocks or count < 2*block_length: out['reasons'].append('INSUFFICIENT_TIME_BLOCKS')
    if out['reasons']: return out
    rng=Random(seed);samples=[]
    for _ in range(repetitions):
        sample=[]
        for _ in range(ceil(count/block_length)):
            start=rng.randrange(count);sample.extend(values[(start+i)%count] for i in range(block_length))
        samples.append(mean(sample[:count]))
    samples.sort();tail=alpha/(2*family_size)
    lo=samples[int(tail*repetitions)];hi=samples[min(repetitions-1,int((1-tail)*repetitions))]
    out.update(confidence_lower=lo,confidence_upper=hi)
    if family_size==1 and alpha==.05:out.update(ci95_lower=lo,ci95_upper=hi)
    if lo>minimum_improvement: out['state']='POSITIVE_CANDIDATE_REQUIRES_HELDOUT_CONFIRMATION'
    elif hi<minimum_improvement: out['state']='REJECTED_FOR_MINIMUM_IMPROVEMENT'
    else: out['reasons'].append('INTERVAL_CROSSES_MINIMUM_IMPROVEMENT')
    return out


def chronological_split(markets: list[dict], train_end_ns: int, validation_end_ns: int,
                        embargo_ns: int = 0) -> dict[str,list[dict]]:
    if not 0 < train_end_ns < validation_end_ns or embargo_ns < 0: raise ValueError('split bounds')
    out={'train':[],'validation':[],'test':[],'purged':[]};seen=set()
    for row in sorted(markets,key=lambda x:x['start_ns']):
        if row['market'] in seen: raise ValueError('duplicate market')
        seen.add(row['market'])
        start,end=row['start_ns'],row['label_end_ns']
        if end < start: raise ValueError('label precedes feature')
        if end < train_end_ns: key='train'
        elif start>=train_end_ns+embargo_ns and end<validation_end_ns: key='validation'
        elif start>=validation_end_ns+embargo_ns: key='test'
        else: key='purged'
        out[key].append(row)
    return out
